"""ScanBook – Oberfläche im Browser.

Starten mit:  python3 ScanBook_Web.py
Danach öffnet sich der Browser automatisch. Beenden mit Strg+C im Terminal.
Die Oberfläche benutzt dieselbe Datenbank wie das Terminal-Programm ScanBook.py.
"""
import io
import os
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
    sql = f"SELECT {sb.BUCH_SPALTEN}, autoren, verlag FROM buecher"
    werte = ()
    if suche:
        muster = f"%{sb.normalize_isbn(suche) or suche}%"
        sql += " WHERE titel LIKE ? OR autoren LIKE ? OR genre LIKE ? OR isbn LIKE ? OR verlag LIKE ? OR serie LIKE ?"
        werte = (muster,) * 6
    with sb.db_connection() as conn:
        rows = conn.execute(sql + " ORDER BY autor_nachname, titel", werte).fetchall()
    return [
        {"autor": r[0], "titel": r[1], "jahr": r[2], "genre": r[3], "gesamt": r[4],
         "verfuegbar": r[5], "isbn": r[6], "serie": r[7] or "", "band": r[8] or "", "autoren": r[9], "verlag": r[10]}
        for r in rows
    ]


def api_book(params):
    """Ein Buch mit allem, was die Vorschau braucht: aktuelle Ausleihen und die anderen Bände der Serie."""
    isbn = isbn_pruefen(params.get("isbn"))
    buch = buch_oder_fehler(isbn)
    with sb.db_connection() as conn:
        buch["ausleihen"] = [
            {"name": name, "bis": bis, "tage": tage_bis(bis)}
            for name, bis in conn.execute(
                "SELECT ausleiher, rueckgabe_bis FROM ausleihen WHERE isbn = ? AND status = 'aktiv' ORDER BY rueckgabe_bis",
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
    return buch


def api_loans(params):
    sql = '''
        SELECT a.id, a.isbn, b.titel, b.autor_formatiert, a.ausleiher, a.ausgeliehen_am, a.rueckgabe_bis,
               COALESCE(a.verlaengerungen, 0)
        FROM ausleihen a JOIN buecher b ON a.isbn = b.isbn
        WHERE a.status = 'aktiv'
    '''
    werte = ()
    if params.get("isbn"):
        sql += " AND a.isbn = ?"
        werte = (isbn_pruefen(params["isbn"]),)
    with sb.db_connection() as conn:
        rows = conn.execute(sql + " ORDER BY a.rueckgabe_bis", werte).fetchall()
    return [
        {"id": r[0], "isbn": r[1], "titel": r[2], "autor": r[3], "ausleiher": r[4],
         "ausgeliehen_am": r[5], "rueckgabe_bis": r[6], "verlaengerungen": r[7], "tage": tage_bis(r[6])}
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
def api_scan(body):
    isbn = isbn_pruefen(body.get("isbn"))
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
        "daten": daten,
        "fehlend": fehlende_felder(daten) if daten else [feld for feld, _ in sb.BUCH_FELDER],
    }


def api_book_create(body):
    isbn = isbn_pruefen(body.get("isbn"))
    if not text(body, "titel"):
        raise ApiFehler("Bitte einen Titel eingeben.")
    if sb.lade_buch(isbn):
        raise ApiFehler("Dieses Buch ist schon im Katalog. Zum Aufstocken einfach scannen.")
    sb.buch_speichern(isbn, buchdaten_aus(body), positive_zahl(body.get("anzahl"), 1),
                      nachgeladen=body.get("nachgeladen") in (True, "1"))
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
    buch = buch_oder_fehler(isbn)
    if buch["anzahl_gesamt"] <= 1:
        raise ApiFehler("Das ist das letzte Exemplar – bitte stattdessen das Buch löschen.")
    with sb.db_connection() as conn:
        geaendert = conn.execute('''
            UPDATE buecher SET anzahl_gesamt = anzahl_gesamt - 1, anzahl_verfuegbar = anzahl_verfuegbar - 1
            WHERE isbn = ? AND anzahl_verfuegbar > 0
        ''', (isbn,)).rowcount
    if not geaendert:
        raise ApiFehler("Alle Exemplare sind ausgeliehen. Erst nach einer Rückgabe kann eins entfernt werden.")
    return buch_oder_fehler(isbn)


def api_book_delete(body):
    isbn = isbn_pruefen(body.get("isbn"))
    buch_oder_fehler(isbn)
    with sb.db_connection() as conn:
        aktiv = conn.execute(
            "SELECT COUNT(*) FROM ausleihen WHERE isbn = ? AND status = 'aktiv'", (isbn,)
        ).fetchone()[0]
        if aktiv:
            raise ApiFehler(f"Das Buch ist noch {aktiv}x ausgeliehen und kann nicht gelöscht werden.")
        conn.execute("DELETE FROM ausleihen WHERE isbn = ?", (isbn,))
        conn.execute("DELETE FROM buecher WHERE isbn = ?", (isbn,))
    return {"ok": True}


def api_checkout(body):
    isbn = isbn_pruefen(body.get("isbn"))
    buch = buch_oder_fehler(isbn)
    ausleiher = text(body, "ausleiher")
    if not ausleiher:
        raise ApiFehler("Bitte den Namen des Ausleihers eingeben.")
    tage = positive_zahl(body.get("tage"), sb.get_einstellung_int("standard_leihdauer", 14))

    ausgeliehen_am = sb.heute()
    rueckgabe_bis = ausgeliehen_am + timedelta(days=tage)

    with sb.db_connection() as conn:
        verfuegbar = conn.execute(
            "UPDATE buecher SET anzahl_verfuegbar = anzahl_verfuegbar - 1 WHERE isbn = ? AND anzahl_verfuegbar > 0",
            (isbn,),
        ).rowcount
        if not verfuegbar:
            raise ApiFehler(f"'{buch['titel']}' ist aktuell vollständig ausgeliehen!")

        # Gleiche Schreibweise wie bei früheren Ausleihen verwenden
        bekannt = conn.execute(
            "SELECT ausleiher FROM ausleihen WHERE LOWER(ausleiher) = LOWER(?) LIMIT 1", (ausleiher,)
        ).fetchone()
        if bekannt:
            ausleiher = bekannt[0]

        conn.execute('''
            INSERT INTO ausleihen (isbn, ausleiher, ausgeliehen_am, rueckgabe_bis, status)
            VALUES (?, ?, ?, ?, 'aktiv')
        ''', (isbn, ausleiher, ausgeliehen_am.isoformat(), rueckgabe_bis.isoformat()))

    return {"titel": buch["titel"], "ausleiher": ausleiher, "rueckgabe_bis": rueckgabe_bis.isoformat()}


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
    return {"pfad": sb.erstelle_backup()}


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
    sb.regal_speichern(regal_id, name, faecher, kapazitaet, sb.genre_liste(text(body, "genres")))
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


GET_ROUTEN = {
    "info": api_info,
    "books": api_books,
    "book": api_book,
    "refresh_status": api_refresh_status,
    "loans": api_loans,
    "borrowers": api_borrowers,
    "borrower": api_borrower,
    "stats": api_stats,
    "settings": api_settings,
    "regale": api_regale,
    "update_check": api_update_check,
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
    "regal_save": api_regal_save,
    "regal_delete": api_regal_delete,
    "regal_move": api_regal_move,
    "regale_uebernehmen": api_regale_uebernehmen,
    "update_install": api_update_install,
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
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📚</text></svg>">
<style>
/* Gestaltung nach Apples Human Interface Guidelines: Systemschrift, Systemfarben,
   gruppierte Listen, Seitenleiste (breit) bzw. Tab-Leiste unten (schmal), Hell/Dunkel automatisch */
:root {
  --bg: #f2f2f7; --surface: #ffffff; --surface-2: #f2f2f7; --fill: rgba(118, 118, 128, .12); --border: rgba(60, 60, 67, .18);
  --text: #000000; --muted: rgba(60, 60, 67, .6); --accent: #007aff; --accent-hover: #0066d6; --accent-soft: rgba(0, 122, 255, .1);
  --danger: #ff3b30; --danger-soft: rgba(255, 59, 48, .12); --warn: #c77c00; --warn-soft: rgba(255, 149, 0, .14); --ok: #248a3d; --ok-soft: rgba(52, 199, 89, .14);
  --bar: rgba(249, 249, 249, .78); --sidebar: rgba(236, 236, 241, .85);
  --shadow: none;
  --radius: 12px;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #000000; --surface: #1c1c1e; --surface-2: #2c2c2e; --fill: rgba(118, 118, 128, .24); --border: rgba(84, 84, 88, .6);
    --text: #ffffff; --muted: rgba(235, 235, 245, .6); --accent: #0a84ff; --accent-hover: #409cff; --accent-soft: rgba(10, 132, 255, .2);
    --danger: #ff453a; --danger-soft: rgba(255, 69, 58, .2); --warn: #ff9f0a; --warn-soft: rgba(255, 159, 10, .18); --ok: #30d158; --ok-soft: rgba(48, 209, 88, .18);
    --bar: rgba(22, 22, 24, .8); --sidebar: rgba(28, 28, 30, .85);
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
  background: var(--sidebar); border-right: .5px solid var(--border);
  -webkit-backdrop-filter: saturate(180%) blur(20px); backdrop-filter: saturate(180%) blur(20px);
}
.header-inner { height: 100%; display: flex; flex-direction: column; padding: 18px 10px 14px; }
.brand { display: flex; flex-direction: column; gap: 2px; padding: 4px 10px 16px; }
.brand h1 { margin: 0; font-size: 20px; font-weight: 700; letter-spacing: -.02em; }
.brand .bib { color: var(--muted); font-size: 13px; }
.brand .version { order: 9; position: absolute; bottom: 16px; left: 20px; color: var(--muted); font-size: 12px; }
nav { display: flex; flex-direction: column; gap: 2px; }
nav button {
  display: flex; align-items: center; gap: 10px; width: 100%; text-align: left;
  border: 0; background: none; color: var(--text); font: inherit; font-size: 14px; font-weight: 500;
  padding: 7px 10px; border-radius: 8px; cursor: pointer; white-space: nowrap;
}
nav button svg { width: 19px; height: 19px; flex: none; color: var(--accent); }
nav button:hover { background: var(--fill); }
nav button.active { background: var(--accent); color: #fff; }
nav button.active svg { color: #fff; }
main { margin-left: 232px; max-width: 1060px; padding: 34px 36px 60px; }
.seiten-titel { margin: 0 0 18px; font-size: 34px; font-weight: 700; letter-spacing: -.025em; line-height: 1.15; }
section { display: none; }
section.active { display: block; animation: rein .25s ease-out; }
/* Schmale Fenster: Titelleiste oben, Tab-Leiste unten wie auf dem iPhone */
@media (max-width: 860px) {
  /* ohne backdrop-filter, sonst hängt die fixierte Tab-Leiste am Kopf statt am Fensterrand */
  header { position: sticky; bottom: auto; right: 0; width: auto; border-right: 0; border-bottom: .5px solid var(--border); background: var(--surface); -webkit-backdrop-filter: none; backdrop-filter: none; }
  .header-inner { padding: 10px 16px; }
  .brand { flex-direction: row; align-items: baseline; gap: 8px; padding: 0; }
  .brand h1 { font-size: 17px; font-weight: 600; }
  .brand .version { display: none; }
  nav {
    position: fixed; left: 0; right: 0; bottom: 0; flex-direction: row; gap: 0; overflow-x: auto; scrollbar-width: none;
    padding: 6px 4px calc(6px + env(safe-area-inset-bottom)); background: var(--bar); border-top: .5px solid var(--border);
    -webkit-backdrop-filter: saturate(180%) blur(20px); backdrop-filter: saturate(180%) blur(20px);
  }
  nav button { flex: 1 0 72px; flex-direction: column; gap: 2px; padding: 4px 2px; font-size: 10px; color: var(--muted); text-align: center; }
  nav button span { overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
  nav button svg { width: 24px; height: 24px; color: inherit; }
  nav button:hover { background: none; }
  nav button.active { background: none; color: var(--accent); }
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
.fach { display: grid; grid-template-columns: 70px 1fr; gap: 4px 12px; padding: 10px 16px; box-shadow: inset 0 .5px 0 var(--border); background: var(--surface); }
.fach-nr { font-weight: 600; font-size: 14px; }
.fach-nr .sub { display: block; font-weight: 400; }
.fach .balken { margin: 0 0 8px; }
.fach .balken.voll > div { background: var(--warn); }
.fach-buecher { display: flex; flex-wrap: wrap; gap: 5px; }
.buch-chip { font-size: 13px; padding: 3px 10px; border-radius: 999px; border: 0; background: var(--fill); cursor: pointer; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.buch-chip:hover { filter: brightness(.95); }
.buch-chip.neu { background: var(--ok-soft); color: var(--ok); }
.buch-chip.umstellen { background: var(--warn-soft); color: var(--warn); }
.legende { display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; color: var(--muted); margin-top: 6px; }
.legende .buch-chip { cursor: default; }
.aufgaben li { display: flex; gap: 8px; flex-wrap: wrap; align-items: baseline; }
.aufgaben .pfad { color: var(--muted); font-size: 13px; }
#update-hinweis { margin-top: 8px; align-self: flex-start; }
@media (max-width: 860px) { #update-hinweis { margin: 0 0 0 auto; } }
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
      <h1>ScanBook</h1><span class="bib" id="bib-name"></span><span class="version" id="version"></span><button class="btn small hidden" type="button" id="update-hinweis"></button>
    </div>
    <nav id="nav">
      <button data-tab="scan"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3"/><path d="M8 8v8M11 8v8M14 8v8M17 8v8"/></svg><span>Scannen</span></button>
      <button data-tab="katalog"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 19.5V5a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v14.5M9 19.5V7a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v12.5"/><path d="m14.5 7.8 2.9-.8a1 1 0 0 1 1.2.7l2.9 10.9a1 1 0 0 1-.7 1.2l-2.9.8a1 1 0 0 1-1.2-.7L14 8.9"/><path d="M3 20h12"/></svg><span>Katalog</span></button>
      <button data-tab="regale"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M3 15h18M7 5.5v2M10 5.5v2M7 11.5v2M14 17.5v2M17 17.5v2"/></svg><span>Regale</span></button>
      <button data-tab="ausleihe"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 4 3 8l4 4M3 8h13M17 20l4-4-4-4M21 16H8"/></svg><span>Ausleihe</span></button>
      <button data-tab="personen"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="9" cy="8" r="3.2"/><path d="M3 19.5c.6-3.2 3-5 6-5s5.4 1.8 6 5"/><circle cx="17" cy="9" r="2.5"/><path d="M17 14c2.2 0 3.7 1.3 4.2 3.6"/></svg><span>Ausleiher</span></button>
      <button data-tab="statistik"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20V11M10 20V5M16 20v-6M3 20h18"/></svg><span>Statistik &amp; Export</span></button>
      <button data-tab="einstellungen"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/></svg><span>Einstellungen</span></button>
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
        <input id="scan-isbn" class="big grow" placeholder="ISBN scannen oder eingeben …" autocomplete="off" inputmode="numeric">
        <button class="btn" type="submit">Hinzufügen</button>
      </form>
      <div class="platz-box hidden" id="platz-box"></div>
      <button class="link" id="manual-btn" type="button">Buch ohne Scan von Hand eintragen</button>
    </div>

    <div class="card hidden" id="new-book-card">
      <h2 id="new-book-title">Neues Buch</h2>
      <p class="hint" id="new-book-hint"></p>
      <form id="new-book-form">
        <div class="nb-vorschau hidden" id="nb-vorschau">
          <div class="cover-box"><img alt="" referrerpolicy="no-referrer"><span class="cover-leer hidden">📖</span></div>
          <div class="sub" id="nb-beschreibung"></div>
        </div>
        <input type="hidden" name="beschreibung"><input type="hidden" name="cover_url"><input type="hidden" name="nachgeladen">
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
        <input id="katalog-suche" class="grow" placeholder="Suchen nach Titel, Autor, Serie, Genre, Verlag oder ISBN …" autocomplete="off">
        <span class="sub" id="katalog-anzahl" style="color:var(--muted)"></span>
      </div>
      <div class="toolbar">
        <p class="hint">Klicke auf ein Buch für die Vorschau. Von dort aus lässt es sich auch bearbeiten.</p>
        <label class="check" style="margin-left:auto"><input type="checkbox" id="serien-gruppieren" checked> Serien zusammenfassen</label>
      </div>
      <div class="nachladen hidden" id="nachladen-box">
        <div class="grow"><span id="nachladen-text"></span><div class="balken hidden" id="nachladen-balken"><div></div></div></div>
        <button class="btn small" type="button" id="nachladen-btn">Alle Bücher nachladen</button>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Autor</th><th>Titel</th><th>Jahr</th><th>Genre</th><th>Bestand</th><th>ISBN</th></tr></thead>
          <tbody id="katalog-body"></tbody>
        </table>
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
    <div class="grid-2" style="margin-bottom:18px">
      <div class="card">
        <h2>Buch ausleihen</h2>
        <p class="hint">Buch scannen, Namen eingeben, fertig.</p>
        <form id="checkout-form">
          <label for="co-isbn">ISBN</label>
          <input id="co-isbn" autocomplete="off" placeholder="ISBN scannen …">
          <div class="book-info" id="co-info">Noch kein Buch gescannt.</div>
          <div class="row" style="margin-top:12px">
            <div class="grow"><label for="co-name">Name des Ausleihers</label><input id="co-name" list="namen-liste" autocomplete="off"></div>
            <div style="width:110px"><label for="co-tage">Tage</label><input id="co-tage" type="number" min="1"></div>
          </div>
          <div class="actions"><button class="btn" type="submit" id="co-btn" disabled>Ausleihen</button></div>
        </form>
        <datalist id="namen-liste"></datalist>
      </div>
      <div class="card">
        <h2>Zurückgeben oder verlängern</h2>
        <p class="hint">Buch scannen – ist es nur einmal ausgeliehen, fragt ScanBook direkt, ob es zurückgegeben werden soll. Sonst wird es unten hervorgehoben.</p>
        <form id="return-form">
          <label for="re-isbn">ISBN</label>
          <input id="re-isbn" autocomplete="off" placeholder="ISBN scannen …">
        </form>
        <div class="book-info" id="re-info">Oder unten in der Liste auswählen.</div>
      </div>
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:14px">
        <h2 class="grow" style="margin:0">Aktuelle Ausleihen <span class="sub" id="loans-summary" style="color:var(--muted);font-weight:400;font-size:14px"></span></h2>
        <input id="loans-filter" style="max-width:260px" placeholder="Filtern nach Name oder Titel …" autocomplete="off">
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Buch</th><th>Ausleiher</th><th>Geliehen am</th><th>Rückgabe bis</th><th>Status</th><th></th></tr></thead>
          <tbody id="loans-body"></tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- AUSLEIHER -->
  <section id="tab-personen">
    <div class="card">
      <h2>Ausleiher-Übersicht</h2>
      <p class="hint">Was hat eine bestimmte Person gerade oder früher ausgeliehen?</p>
      <input id="personen-suche" placeholder="Name suchen …" autocomplete="off">
      <div class="chips" id="personen-liste"></div>
    </div>
    <div id="person-detail"></div>
  </section>

  <!-- STATISTIK -->
  <section id="tab-statistik">
    <div class="tiles" id="stat-tiles"></div>
    <div class="grid-2">
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
        <p class="hint">Einmal täglich wird automatisch gesichert. Hier kannst du zusätzlich ein Backup erstellen.</p>
        <button class="btn secondary" type="button" id="backup-btn">Backup jetzt erstellen</button>
      </div>
    </div>
  </section>

  <!-- EINSTELLUNGEN -->
  <section id="tab-einstellungen">
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
    <div class="chips" id="rg-genre-chips"></div>
    <div class="actions">
      <button class="btn" type="submit">Speichern</button>
      <button class="btn secondary" type="button" id="rg-close" style="margin-left:auto">Abbrechen</button>
    </div>
  </form>
</dialog>

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
async function versuche(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); }
}

// --- Navigation ---
const LADER = {
  scan: () => $("#scan-isbn").focus(),
  katalog: () => ladeKatalog(),
  regale: () => ladeRegale(),
  ausleihe: () => { ladeAusleihen(); ladeNamen(); $("#co-isbn").focus(); },
  personen: () => ladePersonen(),
  statistik: () => ladeStatistik(),
  einstellungen: () => ladeEinstellungen(),
};
function zeigeTab(name) {
  if (!LADER[name]) name = "scan";
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $("#seiten-titel").textContent = $(`nav button[data-tab="${name}"]`).textContent;
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
  $("#scan-isbn").focus();
}

$("#scan-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const feld = $("#scan-isbn");
  const isbn = feld.value.trim();
  if (!isbn) return;
  feld.value = "";
  versuche(async () => {
    feld.disabled = true;
    try {
      const r = await post("scan", { isbn });
      if (r.status === "aufgestockt") {
        const b = r.buch;
        toast(`„${b.titel}“ aufgestockt – jetzt ${b.anzahl_gesamt}×`);
        logEintrag(`➕ <b>${esc(b.titel)}</b> aufgestockt (${b.anzahl_verfuegbar}/${b.anzahl_gesamt} verfügbar)${b.standort ? ` · 📍 ${esc(b.standort)}` : ""}`);
        zeigePlatz(b);
      } else if (r.daten) {
        zeigeNeuesBuch(r.isbn, r.daten, r.fehlend,
          r.fehlend.length ? "Gefunden! Gelb markierte Angaben fehlen – du kannst sie ergänzen. [Enter] speichert." : "Gefunden! Bitte kurz prüfen. [Enter] speichert.");
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
    const platz = b.standort_soll || b.standort;
    logEintrag(`✨ <b>${esc(b.titel)}</b> neu hinzugefügt (${b.anzahl_gesamt}×)${platz ? ` · 📍 ${esc(platz)}` : ""}`
      + (b.umstellen ? ` · ↪️ ${b.umstellen} Buch${b.umstellen === 1 ? "" : "er"} umstellen (Tab „Regale“)` : ""));
    zeigePlatz(b);
    schliesseNeuesBuch();
    if (b.serie) get("books").then(setzeSerienListe).catch(() => {});
  });
});

// --- Katalog ---
const offeneSerien = new Set();
$("#serien-gruppieren").checked = speicherLesen("serienGruppieren", "1") === "1";

function buchZeile(b, alsBand = false) {
  const titel = alsBand
    ? `${b.band ? `<span class="band-nr">${esc(b.band)}</span>` : ""}<b>${esc(b.titel)}</b>`
    : `<b>${esc(b.titel)}</b>${b.serie ? `<div class="serie-tag">📚 ${esc(serieText(b.serie, b.band))}</div>` : ""}`;
  const verlag = !alsBand && b.verlag && b.verlag !== "N/A" ? `<div class="sub">${esc(b.verlag)}</div>` : "";
  return `
      <tr class="clickable${alsBand ? " band-zeile" : ""}" data-isbn="${esc(b.isbn)}">
        <td>${esc(b.autor)}</td>
        <td>${titel}${verlag}</td>
        <td class="num">${esc(b.jahr)}</td>
        <td>${esc(b.genre)}</td>
        <td class="num"><span class="badge ${b.verfuegbar > 0 ? "ok" : "danger"}">${b.verfuegbar}/${b.gesamt} verfügbar</span></td>
        <td class="num sub">${esc(b.isbn)}</td>
      </tr>`;
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
        <td>${esc(autoren.length > 2 ? autoren.slice(0, 2).join(", ") + " u. a." : autoren.join(", "))}</td>
        <td><span class="pfeil">▸</span> <b>📚 ${esc(g.serie)}</b><div class="sub">${g.baende.length} Bände im Katalog</div></td>
        <td class="num">${jahre.length ? (von === bis ? von : `${von}–${bis}`) : ""}</td>
        <td>${esc(genres.length > 1 ? genres[0] + " …" : genres[0] || "")}</td>
        <td class="num"><span class="badge ${verfuegbar > 0 ? "ok" : "danger"}">${verfuegbar}/${gesamt} verfügbar</span></td>
        <td class="num sub">${offen ? "zuklappen" : "aufklappen"}</td>
      </tr>` + (offen ? g.baende.map((b) => buchZeile(b, true)).join("") : "");
}

let katalog = [];
function zeichneKatalog() {
  const suche = $("#katalog-suche").value.trim();
  let html = "";
  if (!$("#serien-gruppieren").checked) {
    html = katalog.map((b) => buchZeile(b)).join("");
  } else {
    // Bände einer Serie an der Stelle des ersten Bandes zusammenfassen
    const gruppen = new Map(), eintraege = [];
    for (const b of katalog) {
      const schluessel = b.serie.trim().toLowerCase();
      if (!schluessel) { eintraege.push(b); continue; }
      if (!gruppen.has(schluessel)) {
        const g = { schluessel, serie: b.serie, baende: [] };
        gruppen.set(schluessel, g);
        eintraege.push(g);
      }
      gruppen.get(schluessel).baende.push(b);
    }
    html = eintraege.map((e) => {
      if (!e.baende) return buchZeile(e);
      if (e.baende.length === 1) return buchZeile(e.baende[0]);
      e.baende.sort(nachBand);
      return serienZeilen(e, Boolean(suche) || offeneSerien.has(e.schluessel));
    }).join("");
  }
  $("#katalog-body").innerHTML = html || `<tr><td colspan="6" class="empty">${suche ? "Keine Treffer." : "Der Katalog ist leer. Scanne dein erstes Buch im Tab „Scannen“."}</td></tr>`;
}

// Vorschläge für das Serien-Feld, damit derselbe Name immer gleich geschrieben wird
function setzeSerienListe(buecher) {
  const serien = [...new Set(buecher.map((b) => b.serie).filter(Boolean))].sort((a, b) => a.localeCompare(b, "de"));
  $("#serien-liste").innerHTML = serien.map((s) => `<option value="${esc(s)}">`).join("");
}

async function ladeKatalog() {
  await versuche(async () => {
    katalog = await get("books", { q: $("#katalog-suche").value });
    $("#katalog-anzahl").textContent = `${katalog.length} Titel`;
    zeichneKatalog();
    if (!$("#katalog-suche").value.trim()) setzeSerienListe(katalog);
  });
  zeigeNachladen(await get("refresh_status").catch(() => null));
}
$("#katalog-suche").addEventListener("input", debounce(ladeKatalog));
$("#serien-gruppieren").addEventListener("change", (e) => {
  speicherSchreiben("serienGruppieren", e.target.checked ? "1" : "0");
  zeichneKatalog();
});
$("#katalog-body").addEventListener("click", (e) => {
  const serie = e.target.closest("tr[data-serie]");
  if (serie) {
    const s = serie.dataset.serie;
    offeneSerien.has(s) ? offeneSerien.delete(s) : offeneSerien.add(s);
    zeichneKatalog();
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
      `<li><span>${esc(a.name)}</span><span class="time">bis ${deDatum(a.bis)}</span>${statusBadge(a.tage)}</li>`).join("");

    const dlg = $("#preview-dialog");
    if (!dlg.open) dlg.showModal();
    dlg.querySelector(".dlg-body").scrollTop = 0;
  });
}
$("#pv-close").addEventListener("click", () => $("#preview-dialog").close());
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
function zeichneRegale(d) {
  regalDaten = d;
  const anzahl = d.regale.length;
  $("#regale-liste").innerHTML = anzahl ? d.regale.map((r, i) => {
    const belegt = r.inhalt.flat().reduce((n, b) => n + b.anzahl, 0);
    const platz = r.faecher * r.kapazitaet;
    const faecher = r.inhalt.map((fach, f) => {
      const n = fach.reduce((s, b) => s + b.anzahl, 0);
      return `<div class="fach"><div class="fach-nr">Fach ${f + 1}<span class="sub">${n}/${r.kapazitaet}</span></div>
        <div><div class="balken ${n >= r.kapazitaet ? "voll" : ""}"><div style="width:${Math.min(100, Math.round(100 * n / r.kapazitaet))}%"></div></div>
        <div class="fach-buecher">${fach.map(buchChip).join("") || `<span class="sub" style="color:var(--muted)">leer</span>`}</div></div></div>`;
    }).join("");
    return `<div class="regal">
      <div class="regal-kopf">
        <div class="grow"><b>${esc(r.name)}</b>
          <div class="sub">${r.faecher} Fächer × ${r.kapazitaet} Bücher · belegt ${belegt}/${platz} · ${r.genres.length ? esc(r.genres.join(", ")) : "alle übrigen Bücher"}</div></div>
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
function genreChips() {
  const feld = $("#rg-genres");
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
  genreChips();
  $("#regal-dialog").showModal();
  form.name.focus();
}
$("#regal-neu").addEventListener("click", () => oeffneRegal(null));
$("#rg-close").addEventListener("click", () => $("#regal-dialog").close());
$("#rg-genres").addEventListener("input", genreChips);
$("#rg-genre-chips").addEventListener("click", (e) => {
  const g = e.target.dataset.genre;
  if (!g) return;
  const feld = $("#rg-genres");
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
    zeichneRegale(await post("regal_save", { ...daten, id: Number(daten.regal_id) || null }));
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
  zeigeTab("einstellungen");
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
  const namen = await get("borrowers").catch(() => []);
  $("#namen-liste").innerHTML = namen.map((n) => `<option value="${esc(n)}">`).join("");
}

async function ladeAusleihen() {
  await versuche(async () => {
    ausleihen = await get("loans");
    const ueber = ausleihen.filter((a) => a.tage < 0).length;
    $("#loans-summary").textContent = `· ${ausleihen.length} insgesamt` + (ueber ? `, ${ueber} überfällig` : "");
    zeichneAusleihen();
  });
}

function zeichneAusleihen() {
  const filter = $("#loans-filter").value.trim().toLowerCase();
  const liste = ausleihen.filter((a) =>
    (!markierteIsbn || a.isbn === markierteIsbn) &&
    (!filter || a.ausleiher.toLowerCase().includes(filter) || a.titel.toLowerCase().includes(filter)));
  $("#loans-body").innerHTML = liste.length ? liste.map((a) => `
    <tr class="${markierteIsbn ? "highlight" : ""}">
      <td><b>${esc(a.titel)}</b><div class="sub">${esc(a.autor)}</div></td>
      <td>${esc(a.ausleiher)}</td>
      <td class="num">${deDatum(a.ausgeliehen_am)}</td>
      <td class="num">${deDatum(a.rueckgabe_bis)}${a.verlaengerungen ? `<div class="sub">${a.verlaengerungen}× verlängert</div>` : ""}</td>
      <td>${statusBadge(a.tage)}</td>
      <td style="white-space:nowrap">
        <button class="btn small" data-return="${a.id}">Zurückgeben</button>
        <button class="btn small secondary" data-extend="${a.id}">Verlängern</button>
      </td>
    </tr>`).join("") : `<tr><td colspan="6" class="empty">${ausleihen.length ? "Keine passenden Ausleihen." : "Keine Bücher ausgeliehen – alle Exemplare sind in der Bücherei."}</td></tr>`;
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

// Ausleihen-Formular
let ausleihBuch = null;
async function pruefeAusleihBuch() {
  const isbn = $("#co-isbn").value.trim();
  const infoBox = $("#co-info");
  ausleihBuch = null;
  $("#co-btn").disabled = true;
  infoBox.classList.remove("error");
  if (!isbn) { infoBox.textContent = "Noch kein Buch gescannt."; return; }
  try {
    const b = await get("book", { isbn });
    if (b.anzahl_verfuegbar <= 0) {
      infoBox.classList.add("error");
      infoBox.textContent = `„${b.titel}“ ist aktuell vollständig ausgeliehen.`;
      return;
    }
    ausleihBuch = b;
    infoBox.innerHTML = `📖 <b>${esc(b.titel)}</b> – ${b.anzahl_verfuegbar} von ${b.anzahl_gesamt} verfügbar`;
    $("#co-btn").disabled = false;
  } catch (e) {
    infoBox.classList.add("error");
    infoBox.textContent = e.message;
  }
}
$("#co-isbn").addEventListener("keydown", async (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  await pruefeAusleihBuch();
  if (ausleihBuch) $("#co-name").focus();
});
$("#co-isbn").addEventListener("change", pruefeAusleihBuch);
$("#checkout-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if (!ausleihBuch) return;
  versuche(async () => {
    const r = await post("checkout", { isbn: ausleihBuch.isbn, ausleiher: $("#co-name").value, tage: $("#co-tage").value });
    toast(`„${r.titel}“ an ${r.ausleiher} ausgeliehen – Rückgabe bis ${deDatum(r.rueckgabe_bis)}`);
    e.target.reset();
    ausleihBuch = null;
    $("#co-btn").disabled = true;
    $("#co-info").textContent = "Noch kein Buch gescannt.";
    await Promise.all([ladeAusleihen(), ladeNamen()]);
    $("#co-isbn").focus();
  });
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
      const treffer = await get("loans", { isbn });
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

// --- Ausleiher ---
let aktivePerson = null;
async function ladePersonen() {
  await versuche(async () => {
    const namen = await get("borrowers", { q: $("#personen-suche").value });
    $("#personen-liste").innerHTML = namen.length
      ? namen.map((n) => `<button class="chip ${n === aktivePerson ? "active" : ""}" data-name="${esc(n)}">${esc(n)}</button>`).join("")
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
get("books").then(setzeSerienListe).catch(() => {});
zeigeTab(location.hash.slice(1));
get("update_check").then(zeigeUpdate).catch(() => {});  // still im Hintergrund, Fehler nicht anzeigen
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()

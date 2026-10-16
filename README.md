# scanbook
Bibliotheken Verwaltungsprogramm


Dies ist ein Programm für die Bücherverwaltung. Leider ist das bisher nur ein Test-Projekt.


Möglicherweise, wird daraus ein Großprojekt, alles Kostenlos und für jeden zugänglich.

## Voraussetzungen

- Python 3.8 oder neuer (keine zusätzlichen Pakete nötig)
- Internetverbindung zum Nachschlagen von Buchdaten (Google Books, ersatzweise Deutsche Nationalbibliothek und Open Library)
- Falls die Buchsuche mit `CERTIFICATE_VERIFY_FAILED` scheitert: `python3 -m pip install certifi` (Windows: `py -m pip install certifi`). ScanBook nutzt das Paket automatisch.
- Optional: ein Barcode-Scanner, der wie eine Tastatur funktioniert (USB oder Bluetooth)

## Installation & Start

1. `ScanBook.py` in einen eigenen Ordner legen.
2. Im Terminal starten:

   ```
   python3 ScanBook.py
   ```

### Oberfläche im Browser (empfohlen)

```
python3 ScanBook_Web.py
```

Der Browser öffnet sich automatisch mit der ScanBook-Oberfläche. Beenden mit Strg+C im Terminal. Die Oberfläche läuft nur auf dem eigenen Rechner (`localhost`) und ist von anderen Geräten im Netzwerk nicht erreichbar. Beide Varianten – Terminal und Browser – benutzen dieselbe Datenbank und können abwechselnd verwendet werden.

Die Datenbank (`buecherei_system.db`), Backups (`backups/`) und Exporte (`exporte/`) werden automatisch im selben Ordner wie das Programm angelegt – egal, von wo aus es gestartet wird.

## Funktionen

| Nr. | Funktion | Beschreibung |
|----|----------|--------------|
| 1 | Bücher scannen | ISBN scannen. Neue Bücher werden online nachgeschlagen, bekannte Bücher aufgestockt. Gefundene Daten können vor dem Speichern geprüft und korrigiert werden. |
| 2 | Von Hand eintragen | Für Bücher, die online nicht gefunden werden. |
| 3 | Katalog | Alle Bücher, sortiert nach Nachname des Autors, Bände einer Serie in der richtigen Reihenfolge. |
| 4 | Suchen | Nach Titel, Autor, Serie, Genre oder ISBN. |
| 5 | Bearbeiten / Löschen | Buchdaten ändern, einzelne Exemplare entfernen oder ein Buch löschen. |
| 6 | Ausleihen | Buch scannen, Name eingeben, fertig. |
| 7 | Zurückgeben | Buch scannen (oder aus der Liste wählen). |
| 8 | Verlängern | Rückgabedatum einer Ausleihe nach hinten schieben. |
| 9 | Ausleih-Übersicht | Alle aktuellen Ausleihen, überfällige werden markiert. |
| 10 | Ausleiher-Übersicht | Was hat eine bestimmte Person gerade oder früher ausgeliehen? |
| 11 | Statistik | Bestand, Ausleihen, überfällige und meistgeliehene Bücher. |
| 12 | Export | Katalog, Ausleihen oder Mahnliste als CSV (öffnet sich direkt in Excel). |
| 13 | Backup | Sicherungskopie der Datenbank. Zusätzlich wird einmal täglich beim Start automatisch gesichert. |
| 14 | Stammdaten | Schulname, Standard-Leihdauer, Löschfrist usw. |
| 15 | Buchdaten nachladen | Sucht für Bücher, die schon im Katalog sind, online nach Beschreibung, Cover und Serie. Von Hand eingetragene Angaben bleiben unverändert. |
| 16 | Updates | Prüft auf GitHub, ob es eine neuere Version gibt. |

## Buchvorschau und Serien

In der Browser-Oberfläche öffnet ein Klick auf ein Buch im Katalog eine Vorschau mit Cover, Beschreibung, Buchdaten, aktuellen Ausleihen und den anderen Bänden derselben Serie.

Serie und Band werden beim Scannen aus Google Books und der Deutschen Nationalbibliothek übernommen, soweit die Quellen sie kennen (Verlagsreihen wie „dtv“ oder „rororo“ werden ignoriert). Fehlt die Serie, lässt sie sich unter „Bearbeiten“ eintragen; das Feld schlägt bereits vorhandene Seriennamen vor, damit alle Bände gleich geschrieben werden. Im Katalog werden die Bände einer Serie zu einer Zeile zusammengefasst, die sich aufklappen lässt („Serien zusammenfassen“ ein- oder ausschalten).

Für Bücher, die schon vor Version 2.3.0 im Katalog waren, gibt es im Katalog den Knopf „Alle Bücher nachladen“ (Terminal: Menüpunkt 15). Er ergänzt nur leere Angaben. Die Cover werden direkt im Browser von Google Books, Open Library oder der DNB geladen.

## ISBN-Prüfung

Jede ISBN wird anhand ihrer Prüfziffer kontrolliert, damit Scan- und Tippfehler sofort auffallen. ISBN-10 werden automatisch in ISBN-13 umgerechnet, damit dasselbe Buch nicht doppelt im Katalog landet.

## Datenschutz

Abgeschlossene Ausleihen werden nach der eingestellten Löschfrist (Standard: 12 Monate) beim Programmstart automatisch gelöscht. Die Frist lässt sich in den Stammdaten ändern (`0` = nie löschen).

Die Datenbank enthält Namen von Ausleihern und wird deshalb über `.gitignore` nicht mit auf GitHub hochgeladen.

## Updates veröffentlichen (für Entwickler)

1. In `ScanBook.py` die Zeile `VERSION = "..."` erhöhen (z. B. `2.0.0` → `2.1.0`).
2. Auf GitHub hochladen.
3. Empfohlen: auf GitHub ein **Release** mit einem Tag erstellen (z. B. `v2.1.0`). Das Programm holt bevorzugt das neueste Release. Gibt es noch keins, wird der `main`-Branch verwendet.

Ein Update ersetzt `ScanBook.py` und `ScanBook_Web.py`. Es wird nur installiert, wenn die Versionsnummer in `ScanBook.py` höher ist und beide Dateien fehlerfrei geladen werden können. Die letzten 3 alten Versionen werden in `backups/` aufbewahrt.

Die Updates werden ohne Anmeldung von GitHub geladen. Das Repository muss deshalb **öffentlich** sein.

## Google Books API-Schlüssel (optional)

Ohne Schlüssel funktioniert die Suche auch, nur mit weniger Abfragen pro Tag. Einen eigenen Schlüssel als Umgebungsvariable `GOOGLE_API_KEY` setzen, niemals in den Code schreiben.

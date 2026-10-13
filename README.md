# scanbook
Bibliotheken Verwaltungsprogramm


Dies ist ein Programm für die Bücherverwaltung. Leider ist das bisher nur ein Test-Projekt.


Möglicherweise, wird daraus ein Großprojekt, alles Kostenlos und für jeden zugänglich.

## Voraussetzungen

- Python 3.8 oder neuer (keine zusätzlichen Pakete nötig)
- Internetverbindung zum Nachschlagen von Buchdaten (Google Books, ersatzweise Open Library)
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

### Windows: Start per Doppelklick

`ScanBook starten.bat` in denselben Ordner wie `ScanBook.py` und `ScanBook_Web.py` legen und doppelklicken. Für ein Desktop-Symbol: Rechtsklick auf die Datei → „Senden an“ → „Desktop (Verknüpfung erstellen)“. Das schwarze Fenster muss offen bleiben, solange ScanBook benutzt wird.

Voraussetzung: Python von python.org, beim Installieren den Haken bei „Add python.exe to PATH“ setzen.

**Ohne Python-Installation:** Beim jeweiligen GitHub-Release `ScanBook-Windows.zip` herunterladen, entpacken (Rechtsklick → „Alle extrahieren“) und im Ordner `ScanBook` die Datei `ScanBook starten.bat` doppelklicken. Das Paket bringt ein eigenes Python mit, es muss nichts installiert werden. Die ZIP wird bei jedem Release automatisch gebaut (`.github/workflows/windows-paket.yml`).

Die Datenbank (`buecherei_system.db`), Backups (`backups/`) und Exporte (`exporte/`) werden automatisch im selben Ordner wie das Programm angelegt – egal, von wo aus es gestartet wird.

## Funktionen

| Nr. | Funktion | Beschreibung |
|----|----------|--------------|
| 1 | Bücher scannen | ISBN scannen. Neue Bücher werden online nachgeschlagen, bekannte Bücher aufgestockt. Gefundene Daten können vor dem Speichern geprüft und korrigiert werden. |
| 2 | Von Hand eintragen | Für Bücher, die online nicht gefunden werden. |
| 3 | Katalog | Alle Bücher, sortiert nach Nachname des Autors. |
| 4 | Suchen | Nach Titel, Autor, Genre oder ISBN. |
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
| 15 | Updates | Prüft auf GitHub, ob es eine neuere Version gibt. |

## ISBN-Prüfung

Jede ISBN wird anhand ihrer Prüfziffer kontrolliert, damit Scan- und Tippfehler sofort auffallen. ISBN-10 werden automatisch in ISBN-13 umgerechnet, damit dasselbe Buch nicht doppelt im Katalog landet.

## Datenschutz

Abgeschlossene Ausleihen werden nach der eingestellten Löschfrist (Standard: 12 Monate) beim Programmstart automatisch gelöscht. Die Frist lässt sich in den Stammdaten ändern (`0` = nie löschen).

Die Datenbank enthält Namen von Ausleihern und wird deshalb über `.gitignore` nicht mit auf GitHub hochgeladen.

## Updates veröffentlichen (für Entwickler)

1. In `ScanBook.py` die Zeile `VERSION = "..."` erhöhen (z. B. `2.0.0` → `2.1.0`).
2. Auf GitHub hochladen.
3. Empfohlen: auf GitHub ein **Release** mit einem Tag erstellen (z. B. `v2.1.0`). Das Programm holt bevorzugt das neueste Release. Gibt es noch keins, wird der `main`-Branch verwendet.

Ein Update wird nur installiert, wenn die Versionsnummer höher ist und der Code fehlerfrei geladen werden kann. Die letzten 3 alten Versionen werden in `backups/` aufbewahrt.

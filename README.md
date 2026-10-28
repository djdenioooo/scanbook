# scanbook
Bibliotheken Verwaltungsprogramm


Dies ist ein Programm für die Bücherverwaltung. Leider ist das bisher nur ein Test-Projekt.


Möglicherweise, wird daraus ein Großprojekt, alles Kostenlos und für jeden zugänglich.

## Voraussetzungen

- Python 3.8 oder neuer (keine zusätzlichen Pakete nötig)
- Internetverbindung zum Nachschlagen von Buchdaten (zuerst Deutsche Nationalbibliothek, dazu gleichzeitig Google Books und Open Library für fehlende Angaben wie das Cover; eine Quelle, die nicht antwortet, wird nach 5 Sekunden aufgegeben und 10 Minuten lang übersprungen)
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

Die Browser-Oberfläche hat sechs Bereiche: **Ausleihe** (Startseite), **Katalog**, **Schüler**, **Erfassen** (neue Bücher scannen), **Regale** und **Verwaltung** (Statistik, Export, Backup, Mediencodes, Einstellungen und Updates). Lange Listen werden stückweise beim Herunterscrollen angezeigt, die Katalogsuche läuft direkt im Browser.

| Nr. | Funktion | Beschreibung |
|----|----------|--------------|
| 1 | Bücher scannen | ISBN scannen. Neue Bücher werden online nachgeschlagen, bekannte Bücher aufgestockt. Gefundene Daten können vor dem Speichern geprüft und korrigiert werden. |
| 2 | Von Hand eintragen | Für Bücher, die online nicht gefunden werden. |
| 3 | Katalog | Alle Bücher, sortiert nach Nachname des Autors, Bände einer Serie in der richtigen Reihenfolge. |
| 4 | Suchen | Nach Titel, Autor, Serie, Genre oder ISBN. |
| 5 | Bearbeiten / Löschen | Buchdaten ändern, einzelne Exemplare entfernen oder ein Buch löschen. |
| 6 | Ausleihen | Buch scannen (Mediencode oder ISBN), dann Schülerausweis scannen oder Namen eingeben. |
| 7 | Zurückgeben | Buch scannen, Mediencode oder ISBN (oder aus der Liste wählen). |
| 8 | Verlängern | Rückgabedatum einer Ausleihe nach hinten schieben. |
| 9 | Ausleih-Übersicht | Alle aktuellen Ausleihen, überfällige werden markiert. In der Browser-Oberfläche oben mit Zahlen und Filtern „Überfällig“ und „Bald fällig“. |
| 10 | Ausleiher-Übersicht | Was hat eine bestimmte Person gerade oder früher ausgeliehen? Im Browser unter „Schüler“ → „Ausleih-Verlauf“. |
| 11 | Statistik | Bestand, Ausleihen, überfällige und meistgeliehene Bücher. |
| 12 | Export | Katalog, Ausleihen oder Mahnliste als CSV (öffnet sich direkt in Excel). Ausleihen und Mahnliste sind nach Klasse sortiert. |
| 13 | Datensicherung (Backup) | Sicherungskopie der Datenbank und zweiten Backup-Ordner festlegen (siehe unten). |
| 14 | Stammdaten | Schulname, Standard-Leihdauer, Löschfrist usw. |
| 15 | Buchdaten nachladen | Sucht für Bücher, die schon im Katalog sind, online nach Beschreibung, Cover und Serie. Von Hand eingetragene Angaben bleiben unverändert. |
| 16 | Regale & Standorte | Regale mit Fächern anlegen; ScanBook sagt für jedes Buch, wo es hingehört (siehe unten). |
| 17 | Updates | Prüft auf GitHub, ob es eine neuere Version gibt. In der Browser-Oberfläche unter „Verwaltung“ → „Programm-Update“. |

## Buchvorschau und Serien

In der Browser-Oberfläche öffnet ein Klick auf ein Buch im Katalog eine Vorschau mit Cover, Beschreibung, Buchdaten, aktuellen Ausleihen und den anderen Bänden derselben Serie.

Serie und Band werden beim Scannen aus Google Books und der Deutschen Nationalbibliothek übernommen, soweit die Quellen sie kennen (Verlagsreihen wie „dtv“ oder „rororo“ werden ignoriert). Fehlt die Serie, lässt sie sich unter „Bearbeiten“ eintragen; das Feld schlägt bereits vorhandene Seriennamen vor, damit alle Bände gleich geschrieben werden. Im Katalog werden die Bände einer Serie zu einer Zeile zusammengefasst, die sich aufklappen lässt („Serien zusammenfassen“ ein- oder ausschalten).

Der Katalog lässt sich oben rechts zwischen „Cover“ und „Liste“ umschalten; die Wahl wird im Browser gemerkt. In der Cover-Ansicht steht jedes Buch mit seinem Cover, ob es da ist und in welchem Regal es steht. Hat ein Buch noch kein gespeichertes Cover, malt ScanBook eins in der Farbe seines Genres. Serien erscheinen als Stapel, ein Klick klappt die Bände auf.

Reihen mit Staffeln (z. B. „Warrior Cats“) bekommen zusätzlich das Feld **Staffel**. Sobald eine Serie eingetragen ist, bietet das Auswahlmenü die Staffeln an, die es in dieser Serie schon gibt; „＋ Neue Staffel …“ legt eine weitere an (eine Zahl wie „2“ wird als „Staffel 2“ angezeigt, ein Name wie „Die Macht der drei“ bleibt, wie er ist). Jede Staffel zählt ihre Bände wieder ab 1. Im Katalog, in der Vorschau und im Regal stehen die Bände dann nach Staffel und Band geordnet, Bände ohne Staffel (z. B. Specials) am Ende.

Für Bücher, die schon vor Version 2.3.0 im Katalog waren, gibt es im Katalog den Knopf „Alle Bücher nachladen“ (Terminal: Menüpunkt 15). Er ergänzt nur leere Angaben. Die Cover werden direkt im Browser von Google Books, Open Library oder der DNB geladen.

## Datensicherung

ScanBook sichert die Datenbank automatisch einmal täglich in den Ordner `backups/` – beim Start und, wenn das Programm über Nacht offen bleibt, auch an den folgenden Tagen. Die automatischen Backups der letzten 14 Tage bleiben erhalten.

Nach jedem Programm-Update sichert ScanBook beim ersten Start außerdem die Datenbank, bevor sie an die neue Version angepasst wird (`backups/update_…db`, die letzten 3 bleiben erhalten). So lässt sich ein Update jederzeit rückgängig machen, ohne Daten zu verlieren.

Unter **Verwaltung → Datensicherung** (Terminal: Menüpunkt 13) kannst du einen zweiten Ordner eintragen, z. B. einen USB-Stick, OneDrive oder ein Netzlaufwerk. Jede Sicherung wird dann zusätzlich dorthin kopiert, damit die Daten auch einen PC-Defekt überstehen. Klappt das Kopieren nicht (Stick nicht eingesteckt), zeigt die Verwaltung einen Hinweis.

Zum Wiederherstellen: ScanBook beenden und die gewünschte Backup-Datei als `buecherei_system.db` in den Programmordner kopieren.

## Regale und Standorte

Im Tab „Regale“ (Terminal: Menüpunkt 16) legst du einmal deine Regale an: Name, Anzahl Fächer, ungefähr wie viele Bücher in ein Fach passen, und welche Genres hineinkommen. Ein Regal ohne Genres nimmt alle übrigen Bücher auf.

ScanBook verteilt die Bücher dann selbst:

- Ein Buch kommt in das erste Regal, das eines seiner Genres hat. Mit den Pfeilen legst du fest, welches Regal zuerst dran ist.
- Hat ein Buch mehrere Genres, bestimmt das zuerst eingetragene den Platz: „Krieg, Geschichte“ kommt ins Krieg-Fach, „Geschichte, Krieg“ zu Geschichte. Gibt es für das erste Genre kein Fach, zählt das nächste. Die Suche findet das Buch unter allen seinen Genres.
- Innerhalb eines Regals wird nach Genre (in der eingetragenen Reihenfolge) und dann nach Autor sortiert. Bände einer Serie stehen zusammen in der richtigen Reihenfolge.
- Einzelne Fächer können eigene Genres bekommen (im Regal-Dialog unter „Einzelne Fächer: eigene Genres und Anzahl Bücher“, Terminal: beim Bearbeiten des Regals). Ein solches Fach bekommt genau diese Bücher, z. B. Fach 1 = Fantasy, Fach 2 = Krimi, die übrigen Fächer das, was zum Regal passt. Ist ein Fach voll, kommen seine Bücher in die übrigen Fächer des Regals und danach ins nächste passende Regal. Haben mehrere Fächer dieselben Genres, werden sie gemeinsam gefüllt.
- Für jedes Fach lässt sich auch eine eigene Anzahl Bücher eintragen (im Regal-Dialog unter „Einzelne Fächer“, Terminal: beim Bearbeiten des Regals), z. B. 45 statt 30 für ein Fach mit schmalen Mangas. Leer = wie das Regal.
- Ist ein Regal voll, kommen die übrigen Bücher ins nächste Regal, das ihr Genre (oder alle übrigen Bücher) aufnimmt. Was nirgends hinpasst, steht unter „Ohne Platz“.

Beim ersten Einräumen werden die Bücher gleichmäßig auf die Fächer verteilt, damit überall Platz für neue bleibt. Wenn alles so im Regal steht, auf „Erledigt – so eingeräumt“ klicken. Danach zeigt ScanBook beim Scannen eines neuen Buchs sofort an, in welches Regal und Fach es gehört. Die übrigen Bücher bleiben stehen, wo sie sind; nur wenn ein Fach zu voll wird, schlägt ScanBook vor, einzelne Bücher ins Nachbarfach umzustellen. Der Standort steht auch in der Buchvorschau und in der Katalogliste (Spalte „Standort“, grau = geplant, noch nicht eingeräumt). Im Katalog lässt sich nach Regal filtern, die Suche findet auch Regalnamen, und der Katalog-Export enthält den Standort.

### Bücher selbst umsortieren

Soll ein Buch an einem bestimmten Platz stehen, stellst du es selbst dorthin. Es bekommt ein 📌 und bleibt in diesem Fach, auch wenn es nach der Sortierung woanders hingehören würde; ScanBook sortiert die übrigen Bücher drumherum und rechnet den Platz mit.

- **Einzelnes Buch:** In der Buchvorschau unter „Standort selbst festlegen“ Regal und Fach wählen, dann „Hierhin stellen“.
- **Mehrere Bücher:** Im Tab „Regale“ auf „Verschieben“ klicken, die Bücher antippen, unten Regal und Fach wählen und „Hierhin stellen“.
- **Per Scan direkt am Regal:** „Per Scan einräumen“ (oder „Scannen“ neben einem Fach), Fach wählen und die Bücher scannen, die dort stehen sollen (ISBN oder Mediencode). Terminal: Menüpunkt 16, dann 7.
- **Wieder automatisch:** löst das 📌, dann sortiert ScanBook das Buch wieder selbst ein (Terminal: Punkt 8).

**Kategorie suchen:** Über den Regalen steht ein Suchfeld. Tippe eine Kategorie ein (z. B. „Manga“ oder „Fantasy“), dann siehst du nur noch die Fächer, in die diese Kategorie gehört oder in denen solche Bücher stehen. Groß-/Kleinschreibung und Umlaute sind egal („Maerchen“ findet „Märchen“). Auch Regalnamen und Buchtitel werden gefunden. Esc leert die Suche.

„Als Liste drucken“ (oben im Tab „Regale“) erzeugt eine Druckseite mit allen Regalen: jedes Regal auf einer eigenen Seite, darin Fach für Fach die Bücher mit Titel, Autor, Genre und Anzahl, dazu ein Kästchen zum Abhaken. Bücher, die noch eingeräumt oder umgestellt werden müssen, sind vermerkt. Mit „Drucken“ neben einem Regal druckst du nur dieses eine.

## Schülerausweise

Im Tab „Schüler“ der Browser-Oberfläche legst du Schüler mit Vorname, Nachname und Klasse an, einzeln oder als ganze Klassenliste („Klassenliste importieren“: Spalten aus Excel kopieren und einfügen oder eine CSV-Datei wählen, Reihenfolge Nachname, Vorname, Klasse). Jeder Schüler bekommt eine Ausweisnummer (z. B. `S0001`). Haben die Schüler schon einen Ausweis mit Barcode, trägst du dessen Nummer einfach als Ausweisnummer ein.

„Ausweise drucken“ erzeugt pro Klasse eine Druckseite mit Ausweisen im Scheckkartenformat (10 pro A4-Seite) mit Name, Klasse und Barcode. Beim Drucken „Tatsächliche Größe“ bzw. 100 % wählen, dann ausschneiden und am besten laminieren.

Ausleihen im Tab „Ausleihe“: Ausweis scannen, dann nacheinander die Bücher scannen. Jedes Buch wird sofort ausgeliehen, man sieht dabei, was der Schüler schon hat und ob etwas überfällig ist. Mit dem nächsten Ausweis geht es mit dem nächsten Schüler weiter. Lehrkräfte und andere ohne Ausweis lassen sich weiterhin per Name eintragen.

## Mediencodes (Etiketten pro Exemplar)

Jedes Exemplar kann eine eigene Nummer bekommen, die als Etikett im Buch klebt. Beim Ausleihen und Zurückgeben scannst du dann den Mediencode statt der ISBN. So weiß ScanBook genau, welches Exemplar wer hat, und die Rückgabe geht ohne Rückfrage. Bücher ohne Mediencode funktionieren weiter wie bisher mit der ISBN.

- **Eigene Etiketten:** Den Code des vorhandenen Etiketts unter „Erfassen“ scannen, danach die ISBN des Buchs. ScanBook ordnet den Code diesem Exemplar zu. Haben schon alle Exemplare einen Code, zählt es als neues Exemplar. Einzelne Codes lassen sich auch in der Buchvorschau eintragen.
- **Von ScanBook erzeugte Codes:** „Codes für alle Exemplare ohne Code erzeugen“ vergibt fortlaufende Nummern (z. B. `M00001`), „Neue Etiketten drucken“ druckt sie auf Etikettenbögen 70 × 37 mm (24 pro A4-Seite, z. B. Zweckform 3474). Angebrochene Bögen lassen sich weiter verwenden.
- In der Buchvorschau stehen alle Codes eines Buchs, wer welches Exemplar gerade hat, und Knöpfe zum Ausmustern einzelner Exemplare.

Die Vorsilben für Ausweisnummern (`S`) und Mediencodes (`M`) lassen sich unter „Verwaltung“ ändern. Barcodes werden als Code 128 erzeugt, den jeder übliche Barcode-Scanner liest.

## Anmeldung und Benutzer

Die Browser-Oberfläche ist mit Benutzername und Passwort geschützt. Beim allerersten Start legt man den **Admin** an und bekommt einen **Notfall-Code** angezeigt, den man ausdrucken und sicher aufbewahren sollte (z. B. im Sekretariat).

| Rolle | Gültig | Darf |
| --- | --- | --- |
| Admin | dauerhaft | alles, auch Einstellungen und alle Zugänge verwalten |
| Lehrkraft | dauerhaft | Ausleihe, Katalog, Regale, Schüler, Updates; Zugänge der Bücherei-Kraft anlegen, sperren, verlängern |
| Bücherei-Kraft | bis 31.07. | Ausleihe, Katalog, Regale, Schüler, Updates |

**Jahreswechsel:** Der Zugang der Bücherei-Kraft endet automatisch am 31.07. Gibt es danach keinen gültigen Bücherei-Zugang mehr, erscheint auf der Anmeldeseite „Ich bin die neue Bücherei-Kraft“. Damit legt sich die neue Kraft ohne Hilfe einen eigenen Zugang an (nur direkt am Bücherei-PC). Admin und Lehrkraft sehen das danach in der Verwaltung und können den Zugang bei Bedarf sperren.

**Passwort vergessen:** Admin oder Lehrkraft setzen unter Verwaltung → Benutzer & Zugänge ein Startpasswort. Ist das Admin-Passwort weg, hilft am Bücherei-PC „Admin-Passwort vergessen?“ mit dem Notfall-Code oder im Terminal:

```
python ScanBook.py --admin-reset
```

Passwörter werden nur als PBKDF2-Hash gespeichert. Nach 5 falschen Versuchen ist die Anmeldung 5 Minuten gesperrt, nach 60 Minuten ohne Klick wird man abgemeldet. Das Terminal-Programm `ScanBook.py` fragt kein Passwort ab, weil es nur direkt am PC läuft.

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

In der Browser-Oberfläche gibt es dafür unter „Verwaltung“ den Bereich „Programm-Update“. Ist eine neue Version da, erscheint oben neben der Versionsnummer ein Knopf. Nach dem Installieren startet ScanBook von selbst neu und die Seite lädt sich neu.

Die Updates werden ohne Anmeldung von GitHub geladen. Das Repository muss deshalb **öffentlich** sein.

## Google Books API-Schlüssel (optional)

Ohne Schlüssel funktioniert die Suche auch, nur mit weniger Abfragen pro Tag. Einen eigenen Schlüssel als Umgebungsvariable `GOOGLE_API_KEY` setzen, niemals in den Code schreiben.

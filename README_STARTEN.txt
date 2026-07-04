EntretienConnect GitHub Starter v159 slim

Windows:
- Start: 0_START_HIER_EntretienConnect.vbs
- Wenn es nicht klappt: 0_START_DIAGNOSE_EntretienConnect.bat

Mac:
- Start: Starten.command

Wichtig:
- Den Ordner _EntretienConnect nicht löschen oder verschieben.
- Der Starter lädt die aktuelle Oberfläche von GitHub:
  https://joelschartz.github.io/EntretienConnect/
- Die App läuft lokal unter:
  http://127.0.0.1:8765/graph.html

v159:
- Élèves (mode e-Bichelchen): Sprachspalte von 96 px auf 80 px verkleinert.
  Dadurch wird das Namensfeld ca. 4 mm länger und der Abstand zwischen
  Sprache und ✕ entspricht dem E-Mail-Layout besser.
- Starter-Update-Strategie erweitert: Neben der Oberfläche kann der Starter
  nun auch lokale Helferdateien über GitHub aktualisieren (Manifest
  starter-manifest.json). Bei Änderungen an server.py / ebichelchen.py
  startet der lokale Helfer automatisch neu; PowerShell-/Batch-Helfer
  werden ebenfalls ersetzt. So musst du den Kollegen für normale Updates
  kein neues Starter-Zip mehr schicken.

v158:
- Envoi: Auch der e-Bichelchen-Hinweis springt jetzt zum Démarrage und
  startet die Verbindung dort (gleiches Muster wie bei Microsoft —
  Verbinden gibt es nur noch an einer Stelle).
- Élèves (e-Bichelchen-Modus): Namensfeld reicht jetzt bis zur
  Sprachspalte; Sprachauswahl sitzt direkt unter der Überschrift.
- Démarrage: Text der Complet-Karte eigenständig formuliert („Écrit les
  messages dans e-Bichelchen et envoie les e-mails de confirmation.“).

v157:
- Envoi-Liste als ausgerichtete Spalten (Tabulator-Prinzip): Name |
  Kontakt | Sprache | Termin (kompakt „mer 15.07 · 14:00“) | Adressen —
  über alle Zeilen exakt untereinander. Lange Adressen werden abgeschnitten.
- „rappel J-1“-Zusatz wieder entfernt (der Rappels-Tab zeigt das bereits).

v156:
- Bug behoben: Der Hinweis „Microsoft n'est pas connecté“ im Envoi-Reiter
  verschwindet jetzt sofort nach der Anmeldung (vorher erst nach
  Reiterwechsel).
- Microsoft-Anmeldung (Code ablesen) nur noch an EINER Stelle: im
  Démarrage. Der Envoi-Hinweis springt per Knopf dorthin und startet
  die Anmeldung automatisch.
- Envoi-Liste kompakter, zweizeilig: fett Name + „ven 10.07 · 14:20“,
  darunter gedämpft „2 contacts · FR · adressen … · rappel J-1“.
  Der Rappel ist damit pro E-Mail sichtbar; die Präfixe
  „Confirmation/Rappel/e-Bichelchen“ entfallen (der Tab sagt es schon).

v155:
- Wichtig — e-Bichelchen-Anmeldung reparierbar: Nach einer fehlgeschlagenen
  Anmeldung (falsches Passwort, Fenster zu früh geschlossen) blockierte die
  halbfertige IAM-Sitzung jede neue EduKey-Sicherheitsanfrage. „Connecter“
  setzt jetzt alles zurück: App-Browser wird komplett geschlossen und sein
  Profil (Cookies) gelöscht — jede Anmeldung startet frisch mit neuem EduKey.
  (Neuer Helfer-Endpunkt /api/eb/reset-session; Starter-Update nötig.)
- Helfer-Statusmeldungen auf Französisch (statt gemischt Deutsch/Französisch).
- Démarrage: Status-Zeilen einheitlich („Non connecté — nécessaire pour …“).
- Élèves (e-Bichelchen-Modus): Abstand über der Tabelle wie im E-Mail-Modus;
  ✕ sitzt jetzt am rechten Rand (spiegelbildlich zur Nummer links).
- „?“ neben „Importer un CSV“ korrekt vertikal zentriert.

v154:
- CSV-Import-Bug behoben (Modus complet): Zeilen ohne e-Bichelchen-
  Treffer werden jetzt immer als neue Schüler angelegt statt ignoriert.
  Fehlende Sprachen übernehmen die „Langue standard pour nouveaux contacts“.
- Modus complet: keine separate E-Bich-Sprachspalte mehr — die Sprache
  des Contact 1 definiert auch die Sprache der e-Bichelchen-Nachricht
  (Contact 1 hat Vorrang; „?“-Hinweis bei der Langue standard).
- Élèves: Lösch-Kachel durch dezentes kleines ✕-Icon ersetzt;
  „Source e-Bichelchen“-Anzeige im e-Bichelchen-Modus entfernt.
- Envoi: Namensspalte wächst mit (bis 280 px), stattdessen werden
  lange E-Mail-Adressen abgeschnitten.
- Démarrage: Kartentexte vereinheitlicht („Se connecte à … et …“).

v153:
- Rappel neu gelöst: Der Tab heißt jetzt „Activer Rappel“ mit Checkbox.
  Standard: aus. Ein Klick aktiviert (blau + Häkchen), daneben erscheinen
  Uhrzeit und Vorlauf; Klick auf den aktiven Tab deaktiviert wieder.
  Bei aktiviertem Rappel zeigt der Tab zugleich die Rappel-Vorlage.
- Trimestre steht jetzt unterhalb der Vorlagen-Tabs (einzeilig).
- Signature-Checkbox, Logo-Dropdown und alles Zugehörige entfernt:
  Signatur und Logo werden direkt im Editor bearbeitet oder gelöscht
  (Löschen wird übernommen; Toast weist auf ↺ zum Wiederherstellen hin;
  ✓ speichert die aktuelle Signatur als persönlichen Standard).
- Démarrage: kurze Erklärung in den drei Modus-Karten (was verbunden wird).

v151:
- Einstellungszeile (Trimestre / Rappel / Signature) sauber ausgerichtet:
  Beschriftungen auf einer Höhe, alle Bedienelemente einheitlich 40 px hoch.
- Kein Springen mehr beim Wechsel e-Bichelchen ↔ E-mail ↔ Rappel:
  Rappel- und Signature-Einstellungen bleiben stehen (sie gelten global
  für E-Mails, nicht pro Vorlage).
- Élèves: Bei leerer Liste kein „Connecter e-Bichelchen“-Button mehr
  (Verbindung läuft über Démarrage); „Importer … depuis e-Bichelchen“
  jetzt links oben in der Buttonzeile.
- Schlägt der e-Bichelchen-Import fehl, erscheint ein Popup und die App
  springt zurück zum Démarrage (dort neu verbinden).

v150:
- Envoi entrümpelt: Karte „Mode d’envoi“ entfernt (der Modus wird im
  Démarrage gewählt und kann dort jederzeit geändert werden). Verbindungs-
  Warnungen erscheinen weiterhin oben im Envoi-Reiter.
- Rappel-Einstellung (Ein/Aus, Uhrzeit, Vorlauf) zu den „Modèles de
  messages“ verschoben — dorthin, wo auch der Rappel-Text steht
  (Muster: Calendly/Bookings gruppieren Erinnerungen bei den Nachrichten).
- Logo-Auswahl in die Signature-Einstellung integriert (Toggle + Dropdown
  nebeneinander); das extra Panel unter dem Editor ist weg.
- Fix: „?“-Tooltip wurde vom Editor-Rahmen abgeschnitten; sitzt jetzt
  neben „Langue du modèle“ und öffnet nach unten.
- Démarrage beruhigt: Chips neutral grau (Farbe nur noch für die
  Auswahl), Verbindungsstatus als neutrale Zeilen mit Punkt statt
  gelber/grüner Banner.

v149:
- Erklärtexte (CSV-Format, Platzhalter/Variablen) durch dezente
  „?“-Icons ersetzt; die Erklärung erscheint beim Überfahren mit der Maus.
- Logo-Auswahl in der Signatur: kompaktes Dropdown (UNESCO / Logo
  personnel… / Aucun) statt drei Buttons.
- Démarrage: Auswahl-Markierung in der Farbe des Modus (grün / blau /
  bernstein) statt überall blau.
- Alle „Réinitialiser …“-Textlinks durch kleine ↺-Icon-Buttons ersetzt
  (Tooltip erklärt die Aktion): Schülerliste und Kalender oben rechts im
  Kartenkopf, Modell/Signatur/einzelne Mails rechtsbündig unter dem
  jeweiligen Editor. „Als Standard speichern“ ist jetzt ein ✓-Icon.

v148:
- Designsprache jetzt in allen Reitern einheitlich (flache Panels statt
  verschachtelter Rahmen-Boxen; Élèves, Calendrier und Envoi angeglichen).
- Envoi aufgeräumt: Formatierungs-Toolbar, Objet und Text bilden eine
  zusammenhängende Editor-Einheit; Trimestre/Signature ohne extra Box;
  flache Toolbar-Buttons.
- Rendering-Fix: Pastell-Hintergrund als fixe Ebene (flüssigeres Scrollen).

v147:
- Neues Design: helle Pastellfarben, ruhiger Apple-ähnlicher Look
  (Segmented-Control-Tabs, flachere Karten, weichere Schatten, System-Font).
- Oberfläche aufs Minimum reduziert: alle Erklär- und Hilfesätze entfernt
  (Untertitel, Karten-Beschreibungen, „Mode : …“-Banner, Modell-Hinweise usw.).
  Warnungen und Statusmeldungen (nicht verbunden, Dubletten, ungültige
  E-Mails, Variablen-Hinweis) bleiben erhalten.
- Startseite: Modus wählen durch Klick auf die Karte (keine
  „Choisir“-Buttons mehr); Auswahl mit Häkchen sichtbar.

v145:
- e-Bichelchen-Import: importierte Schüler starten jetzt immer auf Französisch
  (die „Langue standard“ gilt nur für manuell/CSV hinzugefügte Schüler).
- Démarrage: „Mode : …“ jetzt blau statt grün.
- e-Bichelchen-Verbindung robuster: „Nouvelle session“ und ein erneutes Verbinden
  schließen jetzt zuerst ein evtl. offen gebliebenes e-Bichelchen-Fenster und leeren
  den Helfer-Status (verhindert das Hängenbleiben beim Neu-Verbinden, v. a. Windows).
  Falls es doch mal klemmt: e-Bichelchen-Fenster schließen, App/Helfer beenden
  (Fenster schließen bzw. „EntretienConnect beenden“) und neu starten.

v144:
- Neue Standardtexte (E-Mail, Rappel, e-Bichelchen) in FR/DE/EN/LU rund um den
  „bilan intermédiaire“; werden beim Update automatisch übernommen (außer eigene
  gespeicherte Standardtexte bleiben erhalten).
- Reiter Élèves: „Réinitialiser la liste d'élèves“ unten (löscht alle Schüler auf einmal).
- Mode complet: vertikale Trennlinie entfernt, Spalte heißt jetzt „E-BICH“ (mehr Platz
  fürs Namensfeld). CSV-Zeile um die e-Bichelchen-Sprache erweitert (6. Spalte, optional).
- „Langue standard pour nouveaux contacts“ wirkt jetzt korrekt auf neue Schüler.
- App-Start: Rappel standardmäßig aus; Trimester wird aus dem Datum abgeleitet
  (Schuljahr ~15.9.–15.7., LU-Ferien: Weihnachten/Ostern je ~2 Wochen).
- Envoi: „Tout sélectionner“ erscheint jetzt auch bei den e-Bichelchen-Meldungen.
- Mac: lokaler Start öffnet den Browser sauberer (kein zusätzliches leeres Fenster).

v143:
- Mode complet: Spalte „e-Bichelchen“ steht jetzt VOR Contact 1 (Reihenfolge wie im
  Versand: e-Bichelchen > E-Mail > Rappel), mit dezenter Trennlinie zum E-Mail-Kanal.
  So liegt die e-Bichelchen-Sprache nicht mehr direkt neben der Sprache von Contact 2.

v142:
- Mode complet (e-Bichelchen + E-Mail): eigene Sprache für die e-Bichelchen-Nachricht.
  Die Élèves-Liste hat jetzt eine zusätzliche Spalte „e-Bichelchen“ — so lassen sich
  pro Schüler 2 E-Mail-Sprachen (je Kontakt) UND 1 eigene e-Bichelchen-Sprache wählen.
  Früher erbte die e-Bichelchen-Nachricht still die Sprache von Kontakt 1.

v141:
- Envoi: einheitliche Info-Zeilen in der Vorschau, am Rappel-Stil ausgerichtet:
  · E-Mail: „Confirmation · le <Termindatum> à <Uhrzeit> · À : <E-Mail>“ (Datum/Uhrzeit
    des Rendez-vous statt nur der E-Mail-Adresse).
  · e-Bichelchen: Label „Contact · FR“ und Zeile „e-Bichelchen · le <Termindatum>“.
  · Rappel bleibt unverändert.

v140:
- Mac: „Starten.command“ ist jetzt ausführbar (executable bit gesetzt). Die Meldung
  „could not be executed because you do not have appropriate access privileges“
  erscheint dadurch beim Doppelklick nicht mehr.
  Falls macOS beim ersten Mal trotzdem warnt (heruntergeladene Datei): einmal
  Rechtsklick auf „Starten.command“ > „Öffnen“ > „Öffnen“.

v139:
- Liste „Élèves e-Bichelchen“ im gleichen Layout wie „Liste des élèves“ (E-Mail):
  Namensfeld geht bis nach hinten, Sprachauswahl (FR) und ✕ stehen mit demselben
  Abstand rechts wie in der E-Mail-Ansicht. Das frühere doppelte Sprachfeld entfällt.
- Envoi: e-Bichelchen-Nachrichten zeigen jetzt die Sprache (z. B. „e-Bichelchen · FR · …“),
  analog zu den E-Mails.
- Envoi: Bereits importierte e-Bichelchen-Schüler bleiben verknüpft, auch wenn der lokale
  Assistent zwischendurch nicht erreichbar ist. Dadurch bleibt „Tout sélectionner“ und das
  Ankreuzen verfügbar; das tatsächliche Veröffentlichen verlangt weiterhin eine aktive
  e-Bichelchen-Verbindung.

v138:
- Der Windows-VBS-Starter öffnet den Browser nicht mehr selbst.
- Der Browser wird erst von PowerShell geöffnet, nachdem der lokale Server gestartet ist.
- Dadurch soll ERR_CONNECTION_REFUSED beim normalen Start vermieden werden.

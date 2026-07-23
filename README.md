# EntretienConnect v349

**Kein sichtbarer Fenstersprung beim Windows-Start.** Chromium erzeugt das
Browser-App-Fenster zunächst minimiert. Sobald das Fensterhandle existiert,
setzt EntretienConnect die endgültige Größe und Position und zeigt es erst dann
an. Das alte kleine Edge-Fenster links sollte dadurch nicht mehr sichtbar sein.

**Login-Abbrüche werden schneller erkannt.** Ein geschlossenes
e-Bichelchen-Fenster wird im Windows-Haupthelfer direkt am sichtbaren
Seitentarget erkannt, ohne dafür den vollständigen Zusatzhelfer zu starten.
Microsoft wird alle 300 ms geprüft und kehrt nach einem manuellen Schließen
innerhalb von deutlich unter einer Sekunde zum einzigen „Connecter“-Knopf
zurück.

**Kurze, einheitliche Loginmeldung.** e-Bichelchen zeigt
„La connexion s’effectue dans une fenêtre séparée.“ Microsoft verwendet
dieselbe Formulierung mit dem Dienstnamen.

**Fenstergeometrie wird sofort nativ durchgesetzt.** Chromium kann bei
Browser-App-Fenstern seine früher gespeicherte Größe über die korrekten
Startparameter stellen. v348 setzt deshalb Größe, Position und Vordergrund
bereits beim ersten vorhandenen Fensterhandle gemeinsam – ohne Maximieren und
ohne spätere zweite Größenänderung.

**Geschlossenes e-Bichelchen-Login wird automatisch abgebrochen.** Unter
Windows bleibt der vorgewärmte Hilfsbrowser unsichtbar aktiv, obwohl sein
Loginfenster geschlossen wurde. Nun wird das echte Seitentarget geprüft. Nach
dem Schließen kehrt die Oberfläche selbstständig zu „Non connecté / Connecter“
zurück.

**Kürzere Login-Anzeigen.** Die doppelte e-Bichelchen-Anleitung entfällt.
Microsoft zeigt im normalen automatischen Anmeldeablauf nur noch einen kurzen
einzeiligen Hinweis; Erklärungen zum alten Gerätecode erscheinen dort nicht
mehr.

**Fenstergröße jetzt unabhängig von der Windows-Anzeigeskalierung.** Bei
125 %, 150 % oder einer anderen Skalierung werden die von Windows gemeldeten
logischen Pixel in die von Chromium erwarteten physischen Pixel umgerechnet.
Auf einem Bildschirm mit 2560 × 1440 Pixeln öffnet sich das App-Fenster dadurch
wirklich mit ungefähr 1715 × 940 Pixeln und mittig – statt erneut nur ungefähr
1200 Pixel breit links zu erscheinen. Die erkannte Skalierung wird im
Startprotokoll festgehalten.

**Schnellerer Windows-Kaltstart.** Bevor noch kein lokaler Helfer läuft, wird
nicht mehr eine Sekunde auf eine aussichtslose HTTP-Anfrage gewartet. Die kurze
Portprüfung entscheidet zuerst; nur bei einem tatsächlich offenen Port wird die
laufende Version abgefragt. GitHub-Dateiprüfungen bleiben im Hintergrund.

**Browser-App-Fenster sofort in passender Größe.** Edge, Chrome, Brave, Vivaldi
und Opera öffnen EntretienConnect weiterhin ohne leeren Tab und ohne
Adressleiste. Das Fenster nutzt etwa zwei Drittel der Arbeitsfläche des aktuellen
Bildschirms und wird direkt zentriert gestartet – auf 2560 × 1440 also ungefähr
1715 × 950 Pixel. Es wird nicht nachträglich maximiert oder umpositioniert.
Firefox besitzt keinen Browser-App-Modus und öffnet ein separates normales
Fenster.

**Keine falsche rote Versionswarnung bei e-Bichelchen.** Antwortet der
PowerShell-Helfer während einer laufenden e-Bichelchen-Anfrage kurz nicht auf die
zusätzliche Versionsabfrage, wird das nicht mehr als angeblich alter Helfer
„v?“ gewertet. Eine Warnung erscheint nur noch, wenn tatsächlich eine
abweichende Versionsnummer zurückkommt.

**e-Bichelchen bleibt beim Moduswechsel angemeldet.** Der reine E-Mail-Modus
deaktiviert lediglich den Kanal in der Oberfläche. Beim späteren Wechsel zu
„Uniquement e-Bichelchen“ oder zum kombinierten Modus wird die gespeicherte
Sitzung nun automatisch und sofort wieder aufgenommen. Abgemeldet wird nur
noch ausdrücklich über „Déconnecter“.

**Immer ohne leeren zweiten Tab.** Chromium startet EntretienConnect bei jedem
Aufruf im App-Modus – unabhängig davon, ob bereits ein Browserfenster offen ist.

**Windows-Fenster zuverlässig im Vordergrund.** Nach dem Öffnen laufen kurze,
nicht blockierende Suchversuche. Sobald das App-Fenster einmal erfolgreich
aktiviert wurde, enden sie; Größe und Position werden dabei nicht verändert.

Die unter Windows geprüfte e-Bichelchen-Sitzungswiederaufnahme aus v339 bleibt
unverändert erhalten. Bei einer noch gültigen gespeicherten Sitzung wird zudem
sofort der letzte erfolgreich gelesene Stand angezeigt; die Prüfung und
Aktualisierung läuft anschließend still im Hintergrund. Dadurch ist
e-Bichelchen beim zweiten Start bereits grün wie auf dem Mac.

**e-Bichelchen reste maintenant connecté sous Windows aussi.** C'était le point resté ouvert en v338, et il a été construit et vérifié directement sur la machine Windows. La cause n'était pas la connexion elle-même, mais ce qu'il en advenait ensuite : Chromium n'écrit jamais sur le disque les cookies de session pure. Ils ne vivaient donc que dans le navigateur auxiliaire — lequel est fermé après la lecture. Au démarrage suivant, la connexion était perdue. Sur Mac, cette perte n'existe plus depuis la v325, parce que les cookies y sont enregistrés après la lecture. Windows procède désormais de la même façon.

Après chaque lecture réussie, les cookies education.lu sont enregistrés, avec la page où ils étaient valables en dernier et l'User-Agent : `%LOCALAPPDATA%\EntretienConnect\eb-session.json`, valable douze heures, accessible au seul utilisateur connecté (héritage désactivé).

Au démarrage suivant, l'interface voit la session mémorisée et se connecte d'elle-même — sans clic et sans fenêtre. Derrière, un navigateur invisible démarre, reçoit les cookies mémorisés **avant** de charger la page, puis exécute exactement la même routine de lecture que la fenêtre visible. C'est pourquoi la catégorie « Message » continue d'être trouvée correctement : elle est découverte dans la page elle-même, elle n'est pas devinée. Les cookies rafraîchis sont réenregistrés dans la foulée, si bien qu'un usage quotidien ne demande plus de se reconnecter sans cesse.

La déconnexion via « Déconnecter » supprime aussi la session mémorisée — sans quoi l'app se reconnecterait silencieusement au démarrage suivant.

Si la connexion silencieuse échoue (session expirée côté serveur, pas de réseau), e-Bichelchen reste simplement gris et un clic sur « Connecter » ouvre la fenêtre de connexion comme avant. La tentative infructueuse dure une dizaine de secondes, se déroule en arrière-plan et ne laisse jamais de fenêtre de navigateur derrière elle. Aucune fenêtre ne s'ouvre jamais d'elle-même.

L'interface savait déjà faire tout cela : depuis la v331, elle interroge la session mémorisée puis appelle `/api/eb/resume`. Sous Windows, cette route n'existait pas — elle répondait « Route e-Bichelchen inconnue ».

Rappel v338 : le dossier de l'Explorateur ne reste plus au premier plan (`AttachThreadInput`, et la tentative est répétée toutes les 0,7 s au lieu d'une seule fois après 2,5 s).

Rappel v337 : boutons de déconnexion pour e-Bichelchen et Microsoft (l'ancien bouton Microsoft se trouvait dans une carte toujours masquée), et le navigateur est lancé directement avec l'adresse au lieu de passer par l'association Windows.

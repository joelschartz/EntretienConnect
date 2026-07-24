# EntretienConnect v359

**Stabil nach Inaktivität und konsistente Schülerverwaltung.** Das native
Mac-App-Fenster bestimmt jetzt selbst die Lebensdauer des lokalen Helfers.
JavaScript-Drosselung nach längerer Inaktivität beendet ihn daher nicht mehr.
Schüler lassen sich in jedem Kommunikationsmodus manuell oder per CSV
vorbereiten, auch ohne Anmeldung. Nach der e-Bichelchen-Anmeldung steht die
Zuordnung direkt beim Schüler; fehlende Zuordnungen werden markiert und
blockieren den e-Bichelchen-Versand mit einer klaren Korrekturanweisung.

Die portable Windows-App wird passend zu v359 neu ausgeliefert. Entpacken und
Doppelklick genügen weiterhin, ohne Installation und ohne Administratorrechte.

## v358




**Vollständige Dialog- und Dateiauswahl-Unterstützung im Mac-App-Fenster.**
Bestätigungen, Hinweise und Eingabefelder werden in v358 als native
macOS-Dialoge angezeigt. CSV-Import, PDF-Anhang und eigenes Logo verwenden eine
native Dateiauswahl. Damit funktionieren auch Aktionen wie
`Déconnecter Microsoft`, die zuvor an einem unsichtbaren
`window.confirm` hängen konnten.

Der Windows-App-Host wird in v358 mit der kompatiblen Basis-Laufzeit für ältere
Prozessoren gebaut. Dadurch beendet sich die portable EXE auf älteren Schul-PCs
nicht mehr vor dem eigentlichen Programmstart. Zusätzlich wird
`%LOCALAPPDATA%\EntretienConnect\EntretienConnect-AppHost-log.txt` gleich
beim Start angelegt. Installation und Administratorrechte sind weiterhin nicht
erforderlich.

## v357

**Eigenes App-Fenster auf Mac und Windows.** Der Mac-Starter hält die
Hauptoberfläche nun selbst in einem nativen WKWebView-Fenster; Safari wird beim
normalen Start nicht mehr geöffnet. Unter Windows bleibt die portable
WebView2-Hülle aus v356 erhalten. Damit erscheinen auf beiden Systemen weder
Adressleiste noch Tabs oder die lokale Adresse `127.0.0.1`.

Microsoft- und e-Bichelchen-Anmeldungen bleiben bewusst getrennt von der
Hauptoberfläche. CSV-, Speicher-, Versand- und GitHub-Update-Logik bleiben
unverändert. Die App wird weiterhin nur vollständig entpackt und per Doppelklick
gestartet – ohne Installation und ohne Administratorrechte.

## v356

**Eigenes Windows-App-Fenster ohne Browser-Tab.** Die Windows-Starterdatei
öffnet EntretienConnect jetzt in einer eigenen portablen Desktop-Hülle auf Basis
von Microsoft Edge WebView2. Beim normalen Start wird kein Standardbrowser mehr
aufgerufen; Adressleiste und leerer zweiter Tab entfallen. Der lokale
PowerShell-Helfer sowie Microsoft-, e-Bichelchen-, CSV-, Speicher- und
Update-Funktionen bleiben unverändert. Die App wird weiterhin nur entpackt und
per Doppelklick gestartet – ohne Installation und ohne Administratorrechte.

Falls WebView2 auf einem einzelnen PC fehlt oder blockiert ist, zeigt die
Starterdatei einen Hinweis und öffnet die lokale Oberfläche als
Kompatibilitäts-Fallback im Standardbrowser.

## v355

**Chromium startet ohne `--new-window`.** Der v354-Diagnoselog hat gezeigt, dass Edge bei geschlossenem Browser selbst eine leere Startseite zusätzlich zur EntretienConnect-Adresse erzeugt hat. Deshalb wird Chromium nun ausschließlich mit der EntretienConnect-URL gestartet. Normaler Browsermodus, Standardprofil, Fenstergeometrie und Login-Popups bleiben unverändert.

## v354

**Kein leerer Start-Tab mehr unter Windows.** EntretienConnect bleibt ein normaler Tab mit Adress- und Tableiste. Ist bereits ein sichtbares Fenster des Standardbrowsers geöffnet, wird die App dort als neuer Tab geöffnet. Ist kein Browserfenster sichtbar, startet der Browser direkt mit genau einem Fenster auf der EntretienConnect-Adresse. Bloße Hintergrundprozesse wie Edge Startup Boost gelten dabei nicht als geöffnetes Browserfenster. App-Modus, Browserprofil, Fenstergeometrie und Login-Popups bleiben unverändert.

## v353

**App öffnet unter Windows wieder als normaler Browser-Tab.** Der Edge-`--app`-
Fenstermodus hat sich als Sackgasse erwiesen: Weil Edge praktisch immer im
Hintergrund läuft (Startup-Boost/Hintergrund-Apps), wird der App-Start an die
bestehende Edge-Instanz übergeben und die Größenvorgaben werden verworfen — das
Fenster öffnete klein. Eine nachträgliche native Größenkorrektur war die eigentliche
Ursache des sichtbaren Fenstersprungs (per Diagnoseprotokoll eindeutig belegt). Ein
frisch erzwungenes Edge wiederum vergrößerte das Microsoft-Login-Popup und blendete
einen Übersetzungshinweis ein. v353 verzichtet deshalb ganz auf den `--app`-Modus und
öffnet die Oberfläche als normalen Tab im Standardbrowser: **kein Fenstersprung mehr**,
und das **Microsoft-Popup bleibt klein**. Preis dafür ist die sichtbare Adressleiste
und eventuell ein zusätzlicher leerer Tab. (Nur Windows; der Mac nutzt WKWebView und war
nie betroffen.)

**Kein sichtbarer Fenstersprung mehr beim Start (Windows).** Chromium merkt sich
im App-Modus seine zuletzt gespeicherte Fenstergeometrie und ignoriert die
Startparameter beim nächsten Öffnen. Die bisherige Korrektur setzte Größe und
Position mit `SetWindowPos` – das ändert bei einem noch minimierten Fenster
jedoch das Wiederherstellungs-Rechteck nicht, sodass `SW_RESTORE` das Fenster
zuerst in Chromiums alter Standardgröße zeigte und es erst danach in die
Zielgröße sprang. v352 blendet das minimierte Fenster stattdessen mit
`SetWindowPlacement` in einem einzigen Schritt ein: Wiederherstellungs-Rechteck
und Anzeigezustand werden gemeinsam gesetzt, sodass das allererste sichtbare
Bild bereits die eigene Zielgröße hat. Die DPI- und Arbeitsbereich-Umrechnung
aus v347 bleibt erhalten. (Nur Windows; der Mac nutzt WKWebView und war nie
betroffen.)

**Normales Browserprofil wiederhergestellt.** Das eigene Chromium-Profil aus
v350 wurde vollständig aus dem Windows-Start entfernt. Dadurch erscheint kein
neuer Übersetzungshinweis mehr, und Microsoft öffnet wieder im kleinen,
zentrierten Login-Popup mit ungefähr 520 × 720 Pixeln.

**Einheitlicher Verbindungshinweis.** Bei e-Bichelchen und Microsoft steht
derselbe kurze blaue Satz direkt unter der jeweiligen Statuszeile:
„La connexion s’effectue dans une fenêtre séparée.“ Bei e-Bichelchen erscheint
er jetzt sofort zusammen mit „Connexion en cours“ und verschwindet beim
Schließen des Loginfensters wieder.

**Hauptfenster vorerst wieder wie v349.** Chromium wird mit dem normalen Profil
und dem bisherigen minimierten App-Start geöffnet. Damit sind die unerwünschten
Nebenwirkungen von v350 zurückgenommen; die sichtbare Startbewegung wird
separat mit einer anderen Technik gelöst.

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

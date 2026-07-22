# EntretienConnect v341

**Windows: Kaltstart ohne leeren Edge-/Chrome-Tab.** Beim vollständig
geschlossenen Chromium-Browser ersetzt `--same-tab` dessen anfängliche
Startseite durch EntretienConnect. Ist bereits ein Browserfenster geöffnet,
kommt weiterhin genau ein neuer Tab hinzu.

**Windows: e-Bichelchen-Sitzung bleibt erhalten.** Nach erfolgreichem Lesen
werden ausschließlich die benötigten `education.lu`-Cookies lokal gespeichert.
Beim nächsten App-Start versucht ein unsichtbarer Browserlauf, die aktuellen
Klassen- und Schülerdaten mit dieser Sitzung neu zu lesen. Die Sitzung wird wie
auf dem Mac höchstens zwölf Stunden behalten; eine abgelaufene Sitzung führt
wieder zur normalen Anmeldung.

**Le dossier de l’Explorateur ne reste plus au premier plan.** La tentative de v336 échouait pour deux raisons. D’abord, Windows refuse `SetForegroundWindow` à un processus qui n’est pas lui-même au premier plan : l’appel restait sans effet. La file d’entrée est désormais rattachée brièvement au thread du premier plan (`AttachThreadInput`), ce qui rend l’appel recevable. Ensuite, il n’y avait qu’**une seule** tentative, 2,5 s après le démarrage : un Edge démarrant à froid n’a alors pas encore chargé la page, la fenêtre ne s’appelle pas encore « EntretienConnect » et n’est donc pas trouvée. La tentative est maintenant répétée toutes les 0,7 s jusqu’à ce que la fenêtre soit réellement devant — au maximum 25 secondes, et plus jamais ensuite, afin de ne pas voler le focus.

Rappel v337 : boutons de déconnexion pour e-Bichelchen et Microsoft (l’ancien bouton Microsoft se trouvait dans une carte toujours masquée), et le navigateur est lancé directement avec l’adresse au lieu de passer par l’association Windows.
